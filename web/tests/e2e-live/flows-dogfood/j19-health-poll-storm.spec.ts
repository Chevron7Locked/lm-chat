/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J19 — an idle tab must not hammer LM Studio's catalog endpoint on every
 * health-badge poll.
 *
 * THE SHIPPED DEFECT: `useLmStudioHealth.ts` polls GET /api/lmstudio/health
 * every 10s (`refetchInterval: 10_000`) from any open tab. The BACKEND'S
 * own re-probe cache for that route (`models_service.py`'s
 * `_HEALTH_PROBE_TTL_SEC`) shipped effectively uncached relative to that
 * cadence, so every single poll forced a fresh upstream
 * GET /v1/models — ~6 real upstream catalog probes per minute from one
 * idle tab, for no observable benefit (LM Studio's loaded-model catalog
 * does not change every 10 seconds). The fix raised the health-probe TTL
 * to 30s (3x the poll interval) so ~2 of every 3 polls now serve from
 * cache, per `_HEALTH_PROBE_TTL_SEC`'s own comment in models_service.py.
 *
 * No prior dogfood journey ever measured request VOLUME over an idle
 * dwell — every journey is busy driving a chat turn, and a redundant
 * background poll leaves the turn's own pass/fail assertions completely
 * unaffected. This journey's entire point is to sit idle and count.
 *
 * INSTRUMENT: `models_service.probe_start` is logged (INFO) on every REAL
 * upstream GET /v1/models call — reliably teed in dogfood mode (backend
 * runs at --log-level info, see j2's/j6's log-level notes). Counting its
 * occurrences over a fixed idle dwell is a direct measurement of upstream
 * request volume, not an inference from timing.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import { countLogLines } from "./_dogfood-helpers";

// Mirrors web/src/hooks/useLmStudioHealth.ts refetchInterval.
const FE_POLL_INTERVAL_MS = 10_000;
// Mirrors src/lmchat/services/models_service.py _HEALTH_PROBE_TTL_SEC.
const HEALTH_PROBE_TTL_SEC = 30;

// NOT model-gated — a real wall-clock idle dwell, long enough to span
// several TTL cycles so the fixed-vs-broken signal isn't one flaky sample.
const DWELL_MS = 95_000;

// Fixed-cache expectation: at most ceil(dwell / TTL) + 1 probes (the +1
// covers the initial cold probe at mount). Old-broken expectation: roughly
// one probe per FE poll tick (dwell / 10s) — nearly double this ceiling.
// Generous margin either side of a scheduling jitter.
const MAX_EXPECTED_PROBES =
  Math.ceil(DWELL_MS / 1000 / HEALTH_PROBE_TTL_SEC) + 2;

test(
  "j19: an idle tab's LM Studio health polling stays cache-bounded, not one " +
    "upstream probe per poll tick (grey-box)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    // NOT model-gated — no chat turn runs in this journey at all.
    test.setTimeout(300_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const chatId = await createChatViaRequest(page, backendURL, "J19 Health Poll Storm");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    // Mount settle — the health badge (useLmStudioStatus/useLmStudioHealth)
    // lives in the persistent app shell, present on every authenticated page.
    await page.waitForTimeout(2_000);

    const baselineCount = countLogLines(backendLogPath, ["models_service.probe_start"]);
    console.log(
      `J19 DIAG: baseline probe_start count=${String(baselineCount)}; dwelling idle for ` +
        `${String(DWELL_MS)}ms (FE poll=${String(FE_POLL_INTERVAL_MS)}ms, backend TTL=` +
        `${String(HEALTH_PROBE_TTL_SEC)}s)...`,
    );

    // THE DWELL — no interaction at all; this is the whole point.
    await page.waitForTimeout(DWELL_MS);

    const afterCount = countLogLines(backendLogPath, ["models_service.probe_start"]);
    const probesDuringDwell = afterCount - baselineCount;
    const impliedPerMinute = (probesDuringDwell / (DWELL_MS / 1000)) * 60;

    test.info().annotations.push({
      type: "j19-probe-volume",
      description:
        `${String(probesDuringDwell)} probe(s) over ${String(DWELL_MS)}ms ` +
        `(~${impliedPerMinute.toFixed(1)}/min); ceiling=${String(MAX_EXPECTED_PROBES)}`,
    });
    console.log(
      `J19 RESULT: ${String(probesDuringDwell)} upstream probe(s) during the idle dwell ` +
        `(~${impliedPerMinute.toFixed(1)}/min, ceiling=${String(MAX_EXPECTED_PROBES)})`,
    );

    expect(
      probesDuringDwell,
      `${String(probesDuringDwell)} upstream catalog probes fired from one idle tab over ` +
        `${String(DWELL_MS)}ms (~${impliedPerMinute.toFixed(1)}/min) — above the ` +
        `${String(MAX_EXPECTED_PROBES)}-probe ceiling a working health-probe cache implies. This ` +
        "is the poll-storm regression signature (cache TTL not honored, or dropped below the " +
        "FE poll interval).",
    ).toBeLessThanOrEqual(MAX_EXPECTED_PROBES);

    assertNoConsoleErrors(collectErrors(), "j19");
  },
);
