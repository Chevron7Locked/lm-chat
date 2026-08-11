/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J6 — the app-executed `web_search` builtin tool actually FIRES and its
 * result reaches the final answer, against a REAL LM Studio in
 * `openai_compat` mode (native mode never offers the tool — see the
 * openai_compat gate on the builtin-tool dispatch).
 *
 * SearXNG is unreachable in this environment, so the spec pins the
 * web_search_provider app-setting to "ddg" directly (see
 * setWebSearchProvider) rather than relying on the automatic
 * searxng→ddg fallback-on-failure: the default resolution order probes
 * SearXNG first, and that probe's timeout adds ~10s per search that a
 * smoke test doesn't need to pay. DDG is what's actually under test here,
 * not a mock. We do not assert on specific result text (DDG results are
 * non-deterministic), only that the tool fired and the model used it.
 *
 * GREY-BOX: a tool call that silently errors can still produce a
 * plausible-looking (hallucinated) final answer, so "the assistant
 * replied" alone would false-green exactly like the fail-soft aux ops J2
 * guards against. We assert on the REAL observable outcomes instead:
 *   2. backend log — none of the three failure markers fired
 *      (`builtin_tools.web_search.{unavailable,error}`,
 *      `agentic.loop.builtin_tool_exception`);
 *   3. UI — the persisted tool-call line (ProcessStream's inline
 *      `<details>`, not a "card") shows the raw tool name `web_search` (via
 *      its `title` attribute — `formatToolName` humanizes the visible
 *      text) at `data-status="success"`, i.e. the SSE tool_call.success
 *      event landed and survived into the stored message. This is the
 *      PRIMARY dispatch proof: the dogfood backend log is filtered to
 *      WARNING+ (see _fixtures.ts), so the INFO-level
 *      `agentic.loop.execute_tool` dispatch marker is never written to it
 *      — only WARNING+ events (e.g. `agentic.loop.connect_failed`,
 *      `...max_rounds_hit`) show up there. A log-based dispatch assertion
 *      was tried against that marker and removed: it timed out even on a
 *      live run where the tool demonstrably fired, because the log level
 *      simply never carries it;
 *   4. the final assistant answer is non-empty and not a browsing refusal.
 * Reverting the openai_compat gate (or breaking the DDG path) should turn
 * (3) red while a naive "assistant replied" check would stay green.
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
  setWebSearchProvider,
  sendTurnAndWait,
  assertNoLogLine,
} from "./_dogfood-helpers";

// A tool round trip adds a real network fetch (DDG, pinned directly via
// setWebSearchProvider) PLUS a second model completion on top of the
// first — budget well past J5's trivial single-completion turn.
const TURN_TIMEOUT_MS = 300_000;

const REFUSAL_PATTERNS = [
  /i can'?t browse/i,
  /i don'?t have access/i,
  /i cannot access the internet/i,
  /i'?m unable to browse/i,
];

test(
  "j6: web_search fires on openai_compat and its result reaches the final answer (grey-box)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    test.setTimeout(600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    // Pin the web-search backend. Defaults to the keyless `ddgs` path (no
    // setup); override with LMCHAT_DOGFOOD_SEARCH_PROVIDER=brave (needs
    // LM_CHAT_BRAVE_API_KEY) for a deterministic, rate-limit-free run.
    const searchProvider = process.env["LMCHAT_DOGFOOD_SEARCH_PROVIDER"] ?? "ddg";
    await configureLmStudio(page, backendURL, fleet.fastId);
    await setWebSearchProvider(page, backendURL, searchProvider);

    // web_search is gated to openai_compat only — native mode never offers it.
    const modeResp = await page.request.patch(
      `${backendURL}/api/settings/lmstudio/endpoint-mode`,
      { data: { endpoint_mode: "openai_compat" } },
    );
    expect(
      modeResp.ok(),
      `endpoint-mode=openai_compat → HTTP ${modeResp.status()}`,
    ).toBe(true);

    const chatId = await createChatViaRequest(page, backendURL, "J6 web_search");
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.waitForFunction(
      () => {
        const sel = document.querySelector<HTMLSelectElement>(
          '[data-testid="chat-header-model-select"]',
        );
        if (sel === null) return false;
        return sel.value !== "" && sel.options.length > 1;
      },
      null,
      { timeout: 30_000 },
    );

    // Nudge a tool-capable model to actually CALL web_search rather than
    // answer from parametric memory: ask for something it cannot know
    // (current conditions) and demand a cited source.
    await sendTurnAndWait(
      page,
      "Use web search to tell me: what is the current weather in Tokyo " +
        "right now? Cite the source URL.",
      TURN_TIMEOUT_MS,
    );

    // GREY-BOX 2 — none of the three failure paths fired. A tool call that
    // errors can still produce a plausible-looking final answer, so
    // absence-of-failure is load-bearing, not redundant with the UI
    // success check in (3).
    assertNoLogLine(
      backendLogPath,
      ["builtin_tools.web_search.unavailable"],
      "web_search executor reported unavailable (config/env not wired)",
    );
    assertNoLogLine(
      backendLogPath,
      ["builtin_tools.web_search.error"],
      "web_search executor raised (WebSearchService call failed)",
    );
    assertNoLogLine(
      backendLogPath,
      ["agentic.loop.builtin_tool_exception", "tool=web_search"],
      "web_search tool call raised inside the agentic loop's backstop handler",
    );

    // GREY-BOX 3 — the tool call surfaced in the UI and PERSISTED as
    // successful. toolCalls come from the message's stored tool_calls once
    // the turn settles (deriveMessageList), so this is the real persisted
    // outcome, not a live-only artifact. formatToolName humanizes the
    // visible label, so match the raw name via the `title` attribute.
    // The model may call web_search MORE THAN ONCE for a single question
    // (observed: 3 queries for one weather ask). A single-element locator then
    // trips Playwright strict mode, so match all web_search tool lines and
    // assert the first persisted as successful (dispatch proof). The
    // backend-log failure-marker checks above already guard against any call
    // erroring, so first-is-success is sufficient for "the tool fired + won".
    const toolLines = page
      .locator(".lmchat-process-tool")
      .filter({ has: page.locator('.lmchat-process-tool__name[title="web_search"]') });
    await expect(toolLines.first()).toHaveAttribute("data-status", "success", {
      timeout: 15_000,
    });

    // GREY-BOX 4 — the final answer is non-empty and not a browsing refusal
    // (the "I can't browse" family a non-tool-using model would give).
    const answer = await page
      .locator(".lmchat-bubble-assistant")
      .last()
      .innerText();
    expect(
      answer.trim().length,
      `assistant answer was empty: ${answer}`,
    ).toBeGreaterThan(0);
    for (const pattern of REFUSAL_PATTERNS) {
      expect(
        pattern.test(answer),
        `assistant answer looked like a browsing refusal: ${answer}`,
      ).toBe(false);
    }

    assertNoConsoleErrors(collectErrors(), "j6");
  },
);
