/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J12 — characterization journey for the stale-capability-legend bug
 * (memory: project_lm_chat_legend_do_section_stale_after_gate_2026_08_14) and its
 * committed-but-unproven fix, `apply_tools_unavailable_corrective` (`prompt_assembly.py`).
 *
 * THE BUG: the system prompt's "[Capabilities] Tools you can call directly" legend
 * (streaming_service.py:~2503-2506) is built from the RAW requested integrations,
 * BEFORE `_resolve_model_and_integrations_gate` (~3136) runs. That gate can silently
 * drop_all (Layer 1: resolved model has capabilities.trained_for_tool_use=False) or
 * trim (Layer 2: the request would overflow the model's loaded context window) —
 * either way the legend keeps advertising a tool the wire request no longer carries.
 * A model that believes it holds a tool it wasn't given has been observed emitting
 * the call as literal JSON text in its prose answer instead of a real tool_call.
 *
 * THE FIX UNDER TEST: `apply_tools_unavailable_corrective` (prompt_assembly.py:249)
 * appends a "[Runtime update: ... NOT available this turn ...]" corrective right
 * after the gate resolves, mirroring the existing tools_now_available/date
 * correctives. It has unit coverage but had NEVER been exercised against a real
 * model before this journey.
 *
 * WHY LAYER 2 (TRIM), NOT LAYER 1 (DROP_ALL): Composer.tsx (~962) hides the entire
 * integrations picker whenever the SELECTED model's capabilities.trained_for_tool_use
 * is false — so forcing drop_all through the live UI needs a model already known to
 * be non-tool-trained (unverifiable without a live LM Studio probe this journey
 * doesn't have credentials for) OR a contrived cross-model-switch localStorage-carry
 * trick. Layer 2 needs no such assumption: `estimate_context_budget`
 * (_token_budget.py) fires purely off live-queryable numbers — a loaded model's
 * context length (GET /api/models) and the number of configured integrations
 * (GET /api/integrations/available), each integration costing a flat
 * MCP_INTEGRATION_SCHEMA_TOKENS=1500 fallback when its server isn't yet connected
 * this session. So: pick any loaded TOOL-TRAINED model (the picker is visible),
 * enable every available integration, and — if the model's context is roomy enough
 * that integrations alone don't tip it over — pad the message. This is real user
 * behaviour (enable a lot of tools on a modest-context local model), not a stubbed
 * gate: the exact failure mode _token_budget.py's own docstring cites
 * ("9 integrations = ~16600 tokens, silent stream death").
 *
 * SIZING MATH (mirrors the gate's own arithmetic so this journey's targeting isn't
 * a guess): headroom=2000 and per-integration cost=1500 are the LIVE constants
 * (_token_budget.py `_RESPONSE_HEADROOM_TOKENS` / `MCP_INTEGRATION_SCHEMA_TOKENS`).
 * A calibrated real-system-prompt figure from tests/services/test_token_budget.py's
 * own comments (~800-2000 tokens for the "general" preset alone) informs the 4000
 * -token SYSTEM_PROMPT_ESTIMATE below — deliberately a generous overestimate so the
 * "fits WITHOUT integrations" pre-check (guards against the terminate outcome,
 * a DIFFERENT gate branch that skips the corrective entirely) stays conservative.
 * The padding formula is algebraically guaranteed to overflow WITH integrations
 * once a candidate model clears that pre-check — see the derivation in
 * `pickTrimCandidate` below.
 *
 * GREY-BOX, characterization only (matches J11's contract): this journey asserts
 * ONLY structural facts — the gate actually fired (Layer-2 trim), proven via the
 * backend's own WARNING-level log line (reliably teed — see j6's log-level note).
 * It does NOT assert the model "behaves well"; the fake-tool-call check is LOGGED
 * as a verdict via test.info().annotations, never gated on.
 *
 * WHAT THIS JOURNEY CANNOT PROVE: there is no wire-payload capture in this app —
 * the corrective's exact text never reaches any log or persisted row, so "the
 * corrective reached the wire" is inferred from control flow, not observed
 * directly: `apply_tools_unavailable_corrective` (streaming_service.py:4185-4198)
 * runs unconditionally, in a straight line with no branch, immediately before the
 * `stream.integrations_trimmed_for_context` warning this journey asserts on — so
 * that log line firing is proof the corrective call executed, not proof of its
 * rendered content.
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
  sendTurnAndWait,
  waitForLogLine,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;

// ---------------------------------------------------------------------------
// Budget-gate constants — MUST mirror src/lmchat/services/_token_budget.py.
// Not re-derived from the backend at runtime (no debug endpoint exposes them),
// so any change to those constants requires updating this journey too.
// ---------------------------------------------------------------------------
const RESPONSE_HEADROOM_TOKENS = 2000; // _token_budget.py `_RESPONSE_HEADROOM_TOKENS`
const MCP_INTEGRATION_SCHEMA_TOKENS = 1500; // _token_budget.py `MCP_INTEGRATION_SCHEMA_TOKENS`
// Generous overestimate of the real turn-1 system prompt (persona + [Context] +
// [Capabilities] legend for every enabled tool). Real observed figures in
// tests/services/test_token_budget.py sit at ~800-2000 tokens for the preset
// alone; padded up for the legend's per-tool DO-section lines.
const SYSTEM_PROMPT_ESTIMATE_TOKENS = 4000;
const MIN_SAFE_INPUT_TOKENS = 200;
// Above this, forcing overflow on this model would mean sending a prompt whose
// LOCAL prefill time risks blowing the turn timeout for reasons unrelated to the
// gate under test. Treated as "cannot force deterministically without being
// invasive" — the journey fails loud instead of attempting it.
const MAX_PRACTICAL_INPUT_TOKENS = 20_000;

interface LiveModel {
  key: string;
  loaded_instance_ids?: string[];
  capabilities?: { trained_for_tool_use?: boolean } | null;
  loaded_context_length?: number;
  max_context_length?: number;
}

function isGeneralLlm(key: string): boolean {
  const k = key.toLowerCase();
  return !k.includes("embed") && !k.includes("coder");
}

/**
 * Pick the cheapest loaded, tool-trained, general LLM on which enabling every
 * available integration is ALGEBRAICALLY GUARANTEED to overflow its context
 * budget, while a zero-integrations request still fits (the terminate/trim
 * fork in `_resolve_model_and_integrations_gate`).
 *
 * Derivation (system=S estimate, floor=F, integrations cost=C, cap=W):
 *   inputTokens = max(F, W - S - C/2)
 *   If uncapped:  total = S + inputTokens + C = W + C/2 > W        → overflow.
 *   If capped:    clamp implies W < S + C/2 + F, and since C/2 < C,
 *                 W < S + F + C  ⇒  total = S + F + C > W          → overflow.
 * So ANY candidate that passes the "fits without integrations" pre-check
 * (`W > S + F`, using the generous overestimate S) is guaranteed to overflow
 * once integrations are added — the pre-check is the only real gate needed.
 *
 * Returns null if no loaded model qualifies within MAX_PRACTICAL_INPUT_TOKENS —
 * callers must treat that as "cannot force this state deterministically here."
 */
function pickTrimCandidate(
  models: LiveModel[],
  integrationsCount: number,
): { model: LiveModel; ctxLen: number; inputTokens: number } | null {
  const integrationsCost = integrationsCount * MCP_INTEGRATION_SCHEMA_TOKENS;
  const candidates = models
    .filter(
      (m) =>
        isGeneralLlm(m.key) &&
        Array.isArray(m.loaded_instance_ids) &&
        m.loaded_instance_ids.length > 0 &&
        m.capabilities?.trained_for_tool_use === true,
    )
    .map((m) => ({
      model: m,
      ctxLen: m.loaded_context_length || m.max_context_length || 0,
    }))
    .filter((c) => c.ctxLen > 0)
    .sort((a, b) => a.ctxLen - b.ctxLen);

  for (const { model, ctxLen } of candidates) {
    const maxWithHeadroom = ctxLen - RESPONSE_HEADROOM_TOKENS;
    // Pre-check: must fit WITHOUT integrations (using the generous estimate) —
    // otherwise this model risks the terminate branch instead of trim.
    if (maxWithHeadroom <= SYSTEM_PROMPT_ESTIMATE_TOKENS + MIN_SAFE_INPUT_TOKENS) {
      continue;
    }
    const inputTokens = Math.max(
      MIN_SAFE_INPUT_TOKENS,
      maxWithHeadroom - SYSTEM_PROMPT_ESTIMATE_TOKENS - Math.floor(integrationsCost / 2),
    );
    if (inputTokens > MAX_PRACTICAL_INPUT_TOKENS) {
      // Smaller-context candidates were tried first; a larger context only
      // needs MORE padding, never less — no point trying the rest.
      break;
    }
    return { model, ctxLen, inputTokens };
  }
  return null;
}

// A JSON tool-call shape leaking into prose: a ```json fence, or a bare
// {"tool": "..."} / {"name": "mcp/..."} object outside any real tool_call.
const FAKE_TOOL_CALL_PATTERN =
  /```json[\s\S]*?```|"tool"\s*:\s*"[^"]+"|"name"\s*:\s*"(mcp\/|web_search)/i;

test(
  "j12: integrations gate Layer-2 trim fires + the tools-unavailable corrective's " +
    "call site runs (grey-box) — model behaviour on the stale legend is LOGGED, not asserted",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);

    // mcp/<server> integrations are LM Studio-native dispatch — surfaced to the
    // composer in "native" endpoint mode (see reference_lm_chat_two_endpoint_modes
    // _tool_surface). Pin it explicitly rather than trust whatever's currently
    // configured, since the composer hides the whole picker in the wrong mode.
    const modeResp = await page.request.patch(
      `${backendURL}/api/settings/lmstudio/endpoint-mode`,
      { data: { endpoint_mode: "native" } },
    );
    expect(modeResp.ok(), `endpoint-mode=native → HTTP ${String(modeResp.status())}`).toBe(
      true,
    );

    const integrationsResp = await page.request.get(
      `${backendURL}/api/integrations/available`,
    );
    expect(
      integrationsResp.ok(),
      `GET /api/integrations/available → HTTP ${String(integrationsResp.status())}`,
    ).toBe(true);
    const availableIntegrations = (await integrationsResp.json()) as {
      value: string;
    }[];
    expect(
      availableIntegrations.length,
      "[dogfood j12] no integrations configured — Layer-2 trim needs at least " +
        "one to inflate the budget. Cannot force this state deterministically here.",
    ).toBeGreaterThan(0);

    const modelsResp = await page.request.get(`${backendURL}/api/models`);
    expect(modelsResp.ok(), `GET /api/models → HTTP ${String(modelsResp.status())}`).toBe(
      true,
    );
    const allModels = (await modelsResp.json()) as LiveModel[];
    const candidate = pickTrimCandidate(allModels, availableIntegrations.length);
    expect(
      candidate,
      "[dogfood j12] no loaded, tool-trained general LLM has a context window " +
        "where enabling every available integration deterministically forces a " +
        "trim within a practical padding size (MAX_PRACTICAL_INPUT_TOKENS=" +
        `${String(MAX_PRACTICAL_INPUT_TOKENS)} tokens). This means the Layer-2 ` +
        "trim path cannot currently be forced through normal app usage on this " +
        "environment's live model fleet — say so and stop, per the task brief.",
    ).not.toBeNull();
    if (candidate === null) return; // unreachable after the assertion above; narrows the type

    test.info().annotations.push({
      type: "j12-trigger-model",
      description:
        `${candidate.model.key} ctx=${String(candidate.ctxLen)} ` +
        `integrations=${String(availableIntegrations.length)} ` +
        `plannedInputTokens=${String(candidate.inputTokens)}`,
    });

    await configureLmStudio(page, backendURL, candidate.model.key);

    const chatId = await createChatViaRequest(page, backendURL, "J12 tool-gate corrective");
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

    // Open the Tools picker and enable EVERY visible integration pill. Query the
    // real DOM count rather than trusting the API list — visibility depends on
    // endpoint-mode/system filtering the API alone doesn't reflect.
    const disclosure = page.getByTestId("integrations-disclosure");
    await expect(
      disclosure,
      "[dogfood j12] Tools picker not rendered — either no integrations are " +
        "visible for this model/mode, or the model isn't tool-trained (composer " +
        "hides the picker per Composer.tsx's trained_for_tool_use gate).",
    ).toBeVisible({ timeout: 10_000 });
    if (!(await disclosure.getAttribute("open"))) {
      await disclosure.locator("summary").click();
    }
    const pills = page.locator('[data-testid^="integration-pill-"]');
    const pillCount = await pills.count();
    expect(
      pillCount,
      "[dogfood j12] no integration pills rendered despite a non-empty " +
        "/api/integrations/available list — visibility filtering hid all of them.",
    ).toBeGreaterThan(0);
    for (let i = 0; i < pillCount; i++) {
      const pill = pills.nth(i);
      if ((await pill.getAttribute("aria-checked")) !== "true") {
        await pill.click();
      }
    }
    await expect(pills.first()).toHaveAttribute("aria-checked", "true");

    // Build the oversized turn. approx_token_count = max(1, utf8_bytes // 4);
    // ASCII filler is 1 byte/char, so chars ≈ tokens × 4.
    const fillerUnit =
      "benign padding text used only to size this turn's token budget. ";
    const paddingChars = candidate.inputTokens * 4;
    const filler = fillerUnit.repeat(
      Math.ceil(paddingChars / fillerUnit.length),
    ).slice(0, paddingChars);
    const question =
      "\n\nSearch for the current top world news headline right now and cite " +
      "your source URL.";

    await sendTurnAndWait(page, filler + question, TURN_TIMEOUT_MS);

    // ---- STRUCTURAL PROOF the gate fired (Layer-2 trim) ----
    // WARNING-level, reliably teed (see j6's log-level note) — logged by
    // streaming_service.py right after apply_tools_unavailable_corrective runs
    // in the same straight-line block (streaming_service.py:4185-4198).
    const gateLine = await waitForLogLine(
      backendLogPath,
      ["stream.integrations_trimmed_for_context", `chat_id=${String(chatId)}`],
      10_000,
    );
    test.info().annotations.push({
      type: "j12-gate-fired",
      description: gateLine,
    });

    // ---- LOGGED, NOT ASSERTED: did the model fake a tool call in prose? ----
    // Characterization only — this journey does not gate on model behaviour.
    const answer = await page
      .locator(".lmchat-bubble-assistant")
      .last()
      .innerText();
    const fakeCallMatch = FAKE_TOOL_CALL_PATTERN.exec(answer);
    test.info().annotations.push({
      type: "j12-model-behavior-verdict",
      description: fakeCallMatch
        ? `FAKE TOOL CALL DETECTED in the final answer: ${fakeCallMatch[0].slice(0, 200)}`
        : "no fake-tool-call shape detected in the final answer",
    });
    // Surfaced in the operator's own run output; test.info().annotations
    // alone can be easy to miss in a terminal.
    console.log(
      `[j12] model behaviour verdict: ${
        fakeCallMatch ? "FAKE TOOL CALL DETECTED" : "no fake tool call detected"
      }`,
    );

    // ---- What this journey CANNOT prove ----
    test.info().annotations.push({
      type: "j12-not-verified",
      description:
        "The corrective's rendered TEXT reaching the wire is inferred from " +
        "control flow (no code path skips apply_tools_unavailable_corrective " +
        "between it and the asserted log line), not observed directly — this " +
        "app has no wire-payload capture mechanism.",
    });

    assertNoConsoleErrors(collectErrors(), "j12");
  },
);
