/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J18 — an agentic tool-calling loop that goes deeper than the OLD shipped
 * cap (8 rounds) must not be cut off prematurely.
 *
 * THE SHIPPED DEFECT: the agentic loop's round cap
 * (`_MAX_TOOL_ROUNDS_PER_TURN` / agentic.py's `_DEFAULT_MAX_ROUNDS`)
 * shipped hard-capped at 8; both now default to 256 (env-overridable via
 * LM_CHAT_MAX_TOOL_ROUNDS_PER_TURN / LM_CHAT_MAX_AGENTIC_ROUNDS). No prior
 * dogfood journey exercises more than a couple of tool rounds in one turn
 * — J6 fires web_search once (occasionally 2-3 times per its own
 * docstring), J12 forces a context-budget TRIM, a completely different
 * gate. A cap regression back to 8 would be invisible to every journey
 * that never asks a model to call a tool more than a few times.
 *
 * GREY-BOX, best-effort forcing (matches J12's convention for state this
 * harness cannot force with certainty on an arbitrary loaded model): the
 * prompt explicitly instructs a multi-step tool-call sequence well past 8
 * rounds. Model cooperation with an explicit "call the tool N separate
 * times" instruction is NOT guaranteed across every possible local model,
 * so the round COUNT reached is LOGGED, not hard-gated. What IS hard-
 * gated, unconditionally: the loop must never terminate via
 * `agentic_max_rounds` / `tool_loop_cap` while the model was still
 * actively making progress (i.e., before round 8) — that failure mode is
 * cap-value-independent and catches a regression to a tiny cap regardless
 * of how many rounds the live model actually cooperates for.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import {
  configureLmStudio,
  setWebSearchProvider,
  sendTurnAndWait,
  assertNoLogLine,
  countLogLines,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;

// The exact value of the old, shipped, broken round cap — reaching a
// dispatched-tool-call count above this without a premature cap-hit is
// this journey's positive signal.
const OLD_BROKEN_ROUND_CAP = 8;

const TOPICS = [
  "renewable energy", "deep sea exploration", "ancient Rome", "quantum computing",
  "coral reefs", "space telescopes", "volcanology", "medieval trade routes",
  "bird migration", "fermentation science", "glacier melt", "radio astronomy",
];

interface LiveModelJ18 {
  key: string;
  loaded_instance_ids?: string[];
  capabilities?: { trained_for_tool_use?: boolean } | null;
}

function isGeneralLlmJ18(key: string): boolean {
  const k = key.toLowerCase();
  return !k.includes("embed") && !k.includes("coder");
}

test(
  "j18: an agentic loop pushed well past the OLD 8-round cap never hits " +
    "agentic_max_rounds/tool_loop_cap prematurely (grey-box, best-effort forcing)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    await setWebSearchProvider(page, backendURL, "ddg");
    const modeResp = await page.request.patch(
      `${backendURL}/api/settings/lmstudio/endpoint-mode`,
      { data: { endpoint_mode: "openai_compat" } },
    );
    expect(
      modeResp.ok(),
      `endpoint-mode=openai_compat → HTTP ${String(modeResp.status())}`,
    ).toBe(true);

    // web_search is only offered to tool-trained models — pick one
    // directly rather than reusing classifyFleet's speed-only filter.
    const modelsResp = await page.request.get(`${backendURL}/api/models`);
    expect(modelsResp.ok(), `GET /api/models → HTTP ${String(modelsResp.status())}`).toBe(
      true,
    );
    const modelsBody = (await modelsResp.json()) as
      | LiveModelJ18[]
      | { models?: LiveModelJ18[] };
    const allModels: LiveModelJ18[] = Array.isArray(modelsBody)
      ? modelsBody
      : (modelsBody.models ?? []);
    const toolTrained = allModels.find(
      (m) =>
        isGeneralLlmJ18(m.key) &&
        Array.isArray(m.loaded_instance_ids) &&
        m.loaded_instance_ids.length > 0 &&
        m.capabilities?.trained_for_tool_use === true,
    );
    expect(
      toolTrained,
      "[dogfood j18] no loaded, tool-trained general LLM — cannot exercise the agentic " +
        "tool loop at all on this fleet.",
    ).not.toBeUndefined();
    if (toolTrained === undefined) return; // unreachable; narrows the type

    await configureLmStudio(page, backendURL, toolTrained.key);

    const chatId = await createChatViaRequest(page, backendURL, "J18 Tool Round Depth");
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

    const instruction =
      `You MUST call the web_search tool exactly ${String(TOPICS.length)} separate times, ` +
      "one call per topic below, each as its OWN tool call — do not batch topics into one " +
      "query, do not skip any, and do not give a final answer until all " +
      `${String(TOPICS.length)} calls are complete. Topics, in order:\n` +
      TOPICS.map((t, i) => `${String(i + 1)}. ${t}`).join("\n");

    // Baseline BEFORE this turn — agentic.loop.max_rounds_hit logs
    // max_rounds only, no chat_id, so it can't be scoped to THIS chat by
    // substring match. The backend log is cumulative across the whole
    // (serial, workers:1) run; a before/after diff correctly attributes
    // any occurrence to this journey's own turn.
    const maxRoundsBaseline = countLogLines(backendLogPath, ["agentic.loop.max_rounds_hit"]);

    await sendTurnAndWait(page, instruction, TURN_TIMEOUT_MS);

    assertNoLogLine(
      backendLogPath,
      ["builtin_tools.web_search.unavailable"],
      "web_search executor reported unavailable (config/env not wired)",
    );

    const dispatchedCount = await page.locator(".lmchat-process-tool").count();
    test.info().annotations.push({
      type: "j18-round-depth",
      description:
        `${String(dispatchedCount)} tool call(s) dispatched this turn ` +
        `(requested ${String(TOPICS.length)}, old broken cap was ${String(OLD_BROKEN_ROUND_CAP)})`,
    });
    console.log(
      `J18 RESULT: ${String(dispatchedCount)} tool call(s) dispatched (model=${toolTrained.key})`,
    );

    if (dispatchedCount <= OLD_BROKEN_ROUND_CAP) {
      test.info().annotations.push({
        type: "j18-inconclusive",
        description:
          `only ${String(dispatchedCount)} tool call(s) were dispatched — the live model did ` +
          `not cooperate with the forced ${String(TOPICS.length)}-call instruction enough to ` +
          `exceed the old broken cap of ${String(OLD_BROKEN_ROUND_CAP)}. The unconditional ` +
          "cap-not-hit check below still applies to whatever depth was reached, but the " +
          "cap-VALUE regression signal (dispatched > 8) is not confirmed on this run.",
      });
    }

    // HARD, cap-value-independent invariant, unconditional regardless of
    // depth reached: the loop must never have been cut off by the cap
    // while tool calls were still succeeding. A cap firing here — at ANY
    // depth — is a regression signal; the annotation above only qualifies
    // how STRONG a signal the depth number itself is.
    const maxRoundsAfter = countLogLines(backendLogPath, ["agentic.loop.max_rounds_hit"]);
    expect(
      maxRoundsAfter - maxRoundsBaseline,
      `agentic_max_rounds fired during this turn (after ${String(dispatchedCount)} dispatched ` +
        `tool call(s); old broken cap was ${String(OLD_BROKEN_ROUND_CAP)}) — the round cap ` +
        "regressed to a small value",
    ).toBe(0);
    assertNoLogLine(
      backendLogPath,
      ["stream.tool_loop_cap_hit", `chat_id=${String(chatId)}`],
      `tool_loop_cap fired after ${String(dispatchedCount)} dispatched tool call(s) (old ` +
        `broken cap was ${String(OLD_BROKEN_ROUND_CAP)}) — the round cap regressed to a small ` +
        "value",
    );

    assertNoConsoleErrors(collectErrors(), "j18");
  },
);
