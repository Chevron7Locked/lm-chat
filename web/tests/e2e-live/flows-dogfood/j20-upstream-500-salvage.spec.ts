/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J20 — PHASE 0: characterize an upstream HTTP 500 from LM Studio killing
 * an in-flight turn BEFORE any content has streamed (defect 8:
 * salvaged_kind: None — the answer lost entirely, not salvaged).
 *
 * WHY A PROXY, NOT THE REAL LM STUDIO DIRECTLY: LM Studio has no supported
 * fault-injection surface — there is no "return HTTP 500 on the next
 * request" knob — and this app must never manage LM Studio's own
 * process/state (operator directive: no LM Studio state management). So
 * the fault is injected at the NETWORK layer instead:
 * web/scripts/dogfood-fault-proxy.mjs sits between the backend and the
 * REAL LM Studio, transparently forwarding every request except ONE armed
 * one, which gets a synthetic error status instead of ever reaching LM
 * Studio. `make dogfood-live-fault` wires the proxy in and runs ONLY this
 * file — the routine `dogfood-live` gate never touches the proxy and is
 * completely unaffected by it.
 *
 * THIS JOURNEY DOES NOT ASSERT THE APP "RECOVERS" A NONEXISTENT ANSWER —
 * there is genuinely nothing to salvage when the upstream call fails
 * before any token arrives (see streaming_service.py's
 * `_release_stuck_draft_impl` → `resolve_terminal_content`: `salvaged_kind`
 * stays `None` when neither `salvage_content` nor `salvage_reasoning`
 * produced usable terminal content). What DOES matter, and IS hard-
 * asserted: the failure must be LOUD (an observable error the user can
 * actually see, not a silently-lost turn) and the app must reach a clean
 * terminal state within a bounded time — never hang waiting for a stream
 * that will never arrive. Exactly HOW today's code represents that failure
 * (which backend log lines fire, the persisted row's resting state) is
 * DIAGNOSTIC-LOGGED, not gated — matching J11/J12's own "characterization,
 * not fix" convention for a scenario this harness can only partially probe.
 *
 * WHAT THIS JOURNEY CANNOT PROVE: whether the SAME failure mid-stream
 * (after some content HAS already accumulated) salvages that partial
 * content correctly — the proxy fires before any bytes reach the backend
 * at all, by design (the deterministic, reproducible case). A mid-stream
 * cut is a genuinely different code path (see J11's disconnect
 * characterization, which covers CLIENT-side disconnects, not an upstream
 * failure after partial content) and would need the proxy to track
 * response byte counts and truncate mid-body — not attempted here.
 */
import { test, expect } from "../_fixtures";
import { attachErrorCollector, loginAndWait, createChatViaRequest } from "../flows/_flow-helpers";
import { classifyFleet, configureLmStudio, countLogLines } from "./_dogfood-helpers";

const FAULT_PROXY_PORT = Number(process.env["DOGFOOD_FAULT_PROXY_PORT"] ?? "18234");
const FAULT_PROXY_URL = `http://127.0.0.1:${String(FAULT_PROXY_PORT)}`;

// NOT model-gated — the armed request never reaches LM Studio at all (the
// proxy answers immediately), so there is no real model latency to budget
// for here, unlike almost every other journey in this suite.
const RESOLUTION_CEILING_MS = 120_000;

async function armFault(status: number): Promise<void> {
  const resp = await fetch(`${FAULT_PROXY_URL}/__dogfood_fault__/arm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  expect(
    resp.ok,
    `[dogfood j20] could not arm the fault proxy at ${FAULT_PROXY_URL} — this journey ` +
      "must be run via `make dogfood-live-fault`, not plain `dogfood-live` (which never " +
      "starts the proxy).",
  ).toBe(true);
}

async function proxyStatus(): Promise<{ armed: boolean; fired: number }> {
  const resp = await fetch(`${FAULT_PROXY_URL}/__dogfood_fault__/status`);
  expect(
    resp.ok,
    `[dogfood j20] could not read fault proxy status at ${FAULT_PROXY_URL}`,
  ).toBe(true);
  return (await resp.json()) as { armed: boolean; fired: number };
}

test(
  "j20: an upstream 500 on the FIRST call of a turn surfaces loudly and resolves " +
    "cleanly — no silent, unbounded loss (PHASE 0, characterization only)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    test.setTimeout(180_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const chatId = await createChatViaRequest(page, backendURL, "J20 Upstream 500 Salvage");
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

    await armFault(500);
    const armedState = await proxyStatus();
    expect(
      armedState.armed,
      "[dogfood j20] fault proxy did not confirm armed state before submitting the turn",
    ).toBe(true);

    const composer = page.getByPlaceholder(/Message/);
    await composer.waitFor({ state: "visible", timeout: 15_000 });
    await composer.fill("This turn's first upstream call is about to be faulted.");
    await page.keyboard.press("Enter");

    // ---- HARD ASSERTION 1 — the failure is LOUD, not silent. This
    // codebase's shipped signal is the stream-error banner
    // (StreamErrorBanner.tsx, data-testid="chat-stream-error"). ----
    await expect(
      page.getByTestId("chat-stream-error"),
      "[dogfood j20] no chat-stream-error banner appeared after the faulted upstream call " +
        "— the failure may be silent",
    ).toBeVisible({ timeout: RESOLUTION_CEILING_MS });

    // ---- HARD ASSERTION 2 — the app reaches a definitive terminal state
    // and never hangs waiting for a stream that will never arrive. ----
    await expect(page.getByTestId("composer-send-btn")).toHaveText(/Send$/, {
      timeout: RESOLUTION_CEILING_MS,
    });

    const finalState = await proxyStatus();
    expect(
      finalState.fired,
      "[dogfood j20] the armed fault never actually fired — the turn's first upstream " +
        "call did not route through the proxy as expected (check " +
        "LMCHAT_DOGFOOD_LMSTUDIO_URL points at the proxy, not LM Studio directly)",
    ).toBe(1);

    // ---- DIAGNOSTIC ONLY below this line — logged for the operator, not
    // gated. Mirrors J11/J12's "characterization" convention: real,
    // structurally-observed facts about TODAY's behavior, not a claim
    // about what SHOULD happen. ----
    const chatResp = await page.request.get(`${backendURL}/api/chats/${String(chatId)}`);
    const chatDetail = (await chatResp.json()) as {
      messages: Array<{ id: number; role: string; state: string; content: string }>;
    };
    const lastAssistant = chatDetail.messages.filter((m) => m.role === "assistant").at(-1);
    const restingStateDesc =
      lastAssistant === undefined
        ? "no assistant row was persisted for the faulted turn at all"
        : `state='${lastAssistant.state}' contentLen=${String(lastAssistant.content.length)}`;
    console.log(`J20 RESULT: assistant row after faulted upstream call: ${restingStateDesc}`);
    test.info().annotations.push({ type: "j20-resting-state", description: restingStateDesc });

    const sawUpstreamErrorLog =
      countLogLines(backendLogPath, ["stream.upstream_error"]) > 0;
    const sawSalvageLog =
      countLogLines(backendLogPath, ["stream.stuck_draft_released"]) > 0;
    const logSignalsDesc =
      `stream.upstream_error seen=${String(sawUpstreamErrorLog)} ` +
      `stream.stuck_draft_released seen=${String(sawSalvageLog)}`;
    console.log(`J20 RESULT: backend log — ${logSignalsDesc}`);
    test.info().annotations.push({ type: "j20-backend-log-signals", description: logSignalsDesc });

    // Deliberately NOT asserting on console errors: a faulted upstream
    // call is expected to produce a caught fetch/stream error client-side
    // — that would be a false failure here, not a regression signal
    // (mirrors J11's same deliberate choice for its disconnect scenarios).
    const errors = collectErrors();
    console.log(
      `J20 DIAG: ${String(errors.length)} console error(s)/pageerror(s) collected ` +
        "(not asserted — a faulted upstream call is expected to log one).",
    );
  },
);
