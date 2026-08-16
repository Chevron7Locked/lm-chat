/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J14 — document upload + RAG retrieval actually reaches a REAL model's
 * final answer. No prior dogfood journey touches documents/RAG at all; the
 * only RAG coverage in the whole repo is flow-27/flow-28 in the STUB
 * e2e-live/flows suite, which intercept the upstream request body a stub
 * server captures — a mechanism that does not exist against a real LM
 * Studio (there is no wire-payload capture in real-upstream mode; see j12's
 * docstring on the same limitation).
 *
 * GREY-BOX DESIGN: since the wire body can't be inspected here, retrieval
 * is proven via INSTRUCTION-FOLLOWING as ground truth (the same technique
 * J4 uses for project custom-instructions) — the document holds a randomly
 * generated secret the model cannot possibly know from training data, and
 * the user's question never contains that secret, only a topical keyword
 * that overlaps with the document text (so FTS5's keyword pass has a real
 * term to match, mirroring flow-27's determinism strategy without relying
 * on the stub's fixed-vector embedding trick). If the model's answer
 * contains the exact secret, the doc chunk demonstrably reached the model's
 * context; a real model cannot fabricate an exact match to a random string
 * it was never given.
 *
 *   j14a: rag_enabled=true on the chat → the secret reaches the answer.
 *   j14b: rag_enabled=false on the chat → the secret does NOT reach the
 *         answer (the exact string is unguessable, so absence is reliable
 *         negative evidence too, not just "the model didn't feel like it").
 *
 * Pipeline + real retrieval outcome only — never asserts unrelated model
 * phrasing.
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
  sendTurnAndWait,
  waitForLogLine,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;

function randomSecret(prefix: string): string {
  const rand = Math.random().toString(36).slice(2, 10).toUpperCase();
  return `${prefix}-${rand}-${String(Date.now())}`;
}

async function uploadDoc(
  page: import("@playwright/test").Page,
  backendURL: string,
  filename: string,
  text: string,
): Promise<void> {
  const resp = await page.request.post(`${backendURL}/api/documents`, {
    multipart: {
      file: {
        name: filename,
        mimeType: "text/plain",
        buffer: Buffer.from(text, "utf-8"),
      },
    },
  });
  expect(resp.ok(), `POST /api/documents → HTTP ${String(resp.status())}`).toBe(true);
  const data = (await resp.json()) as { id: number; chunk_count: number };
  expect(
    data.chunk_count,
    "uploaded document produced zero chunks — embedding/chunking failed",
  ).toBeGreaterThan(0);
}

async function setRagEnabled(
  page: import("@playwright/test").Page,
  backendURL: string,
  chatId: number,
  enabled: boolean,
): Promise<void> {
  const resp = await page.request.patch(`${backendURL}/api/chats/${String(chatId)}`, {
    form: { rag_enabled: String(enabled) },
  });
  expect(
    resp.ok(),
    `PATCH rag_enabled=${String(enabled)} → HTTP ${String(resp.status())}`,
  ).toBe(true);
}

test(
  "j14a: an uploaded document's content reaches a real model's answer when RAG is enabled",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const SECRET = randomSecret("J14A");
    // "reference document XJ14QP" is the keyword bridge between doc and
    // query for the FTS5 keyword pass; SECRET itself never appears in the
    // query, only in the document.
    const DOC_TEXT =
      "Reference document XJ14QP. The one-time confirmation code recorded " +
      `in reference document XJ14QP is: ${SECRET}. When asked for the ` +
      "confirmation code from reference document XJ14QP, answer with " +
      "exactly that code.\n";

    await uploadDoc(page, backendURL, "j14a-secret.txt", DOC_TEXT);

    const chatId = await createChatViaRequest(page, backendURL, "J14a RAG Chat");
    await setRagEnabled(page, backendURL, chatId, true);

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

    await sendTurnAndWait(
      page,
      "What is the one-time confirmation code recorded in reference " +
        "document XJ14QP? Reply with ONLY the code, nothing else.",
      TURN_TIMEOUT_MS,
    );

    const answer = await page
      .locator('[data-message-role="assistant"]')
      .last()
      .innerText();
    expect(
      answer,
      `RAG-enabled chat's answer did not contain the retrieved secret. ` +
        `Full answer: ${answer}`,
    ).toContain(SECRET);

    assertNoConsoleErrors(collectErrors(), "j14a");
  },
);

test(
  "j14b: a chat with rag_enabled=false never surfaces the document's content, even when asked",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const SECRET = randomSecret("J14B");
    const DOC_TEXT =
      "Reference document ZQ88NR. The one-time confirmation code recorded " +
      `in reference document ZQ88NR is: ${SECRET}. When asked for the ` +
      "confirmation code from reference document ZQ88NR, answer with " +
      "exactly that code.\n";

    await uploadDoc(page, backendURL, "j14b-secret.txt", DOC_TEXT);

    const chatId = await createChatViaRequest(page, backendURL, "J14b NoRAG Chat");
    await setRagEnabled(page, backendURL, chatId, false);

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

    await sendTurnAndWait(
      page,
      "What is the one-time confirmation code recorded in reference " +
        "document ZQ88NR? Reply with ONLY the code, nothing else, or say " +
        "you don't have that information.",
      TURN_TIMEOUT_MS,
    );

    const answer = await page
      .locator('[data-message-role="assistant"]')
      .last()
      .innerText();
    // A real model cannot fabricate an exact match to a random 8-char
    // string it was never shown — absence is reliable negative evidence
    // that retrieval did not fire, not just "the model declined to answer".
    expect(
      answer,
      `rag_enabled=false leaked the document's secret into the answer ` +
        `anyway. Full answer: ${answer}`,
    ).not.toContain(SECRET);

    assertNoConsoleErrors(collectErrors(), "j14b");
  },
);

// A shipped defect computed the RAG context budget from a STATIC
// 16384-token table regardless of the loaded model's real context window
// — retrieved context silently truncated to a sixteenth on a large-context
// model. j14a/j14b above prove retrieval REACHES the model at all (binary
// pass/fail on a tiny corpus); neither can see a budget that's merely
// SMALLER than it should be, since the tiny corpus fits under either
// budget. j14c proves MAGNITUDE: the applied budget tracks the model's
// LIVE context window, read directly off the backend's own
// `stream.rag_context_trimmed` log line rather than inferred from which
// secret survived.
interface LiveModelJ14 {
  key: string;
  loaded_instance_ids?: string[];
  loaded_context_length?: number;
  max_context_length?: number;
}

function isGeneralLlmJ14(key: string): boolean {
  const k = key.toLowerCase();
  return !k.includes("embed") && !k.includes("coder");
}

// Mirrors src/lmchat/utils/text_input_policy.py PIN_TEXT_MAX_LENGTH.
const PIN_TEXT_MAX_LENGTH = 8192;
// Mirrors src/lmchat/config.py lm_chat_pinned_insights_cap's default.
const PIN_CAP = 100;
// Mirrors rag_mode_resolver.py _RAG_CONTEXT_BUDGET_FRACTION.
const RAG_CONTEXT_BUDGET_FRACTION = 0.25;
// Mirrors rag_service.py _CHARS_PER_TOKEN.
const CHARS_PER_TOKEN = 3.0;
// The shipped defect's static floor (rag_service.py
// _UNKNOWN_CTX_WINDOW_FLOOR_TOKENS) — used here only as the REGRESSION
// SIGNATURE to compare against, not as this journey's own fallback.
const OLD_STATIC_FLOOR_TOKENS = 16_384;
const OLD_STATIC_BUDGET_CHARS = Math.floor(
  OLD_STATIC_FLOOR_TOKENS * RAG_CONTEXT_BUDGET_FRACTION * CHARS_PER_TOKEN,
);

test(
  "j14c: the RAG context budget scales with the model's REAL loaded context " +
    "window, not a static 16384-token floor (grey-box magnitude check)",
  async ({ page, backendURL, backendLogPath, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);

    const modelsResp = await page.request.get(`${backendURL}/api/models`);
    expect(
      modelsResp.ok(),
      `GET /api/models → HTTP ${String(modelsResp.status())}`,
    ).toBe(true);
    const modelsBody = (await modelsResp.json()) as
      | LiveModelJ14[]
      | { models?: LiveModelJ14[] };
    const allModels: LiveModelJ14[] = Array.isArray(modelsBody)
      ? modelsBody
      : (modelsBody.models ?? []);
    const active = allModels.find(
      (m) => m.key === fleet.fastId && isGeneralLlmJ14(m.key),
    );
    const ctxWindow =
      active?.loaded_context_length || active?.max_context_length || 0;
    expect(
      ctxWindow,
      `[dogfood j14c] could not resolve a live context window for ` +
        `${fleet.fastId} — GET /api/models did not report ` +
        "loaded_context_length/max_context_length.",
    ).toBeGreaterThan(0);
    // The old static defect and the fixed dynamic formula agree exactly at
    // 16384 tokens of context — no separation to detect a regression by.
    expect(
      ctxWindow,
      `[dogfood j14c] the loaded model's context window (${String(ctxWindow)} tokens) is not ` +
        `above the OLD static floor (${String(OLD_STATIC_FLOOR_TOKENS)}) — the dynamic and ` +
        "static budgets compute the same value here, so a regression back to the static " +
        "table would be invisible to this journey. Load a model with a larger context " +
        "window to get real coverage.",
    ).toBeGreaterThan(OLD_STATIC_FLOOR_TOKENS);

    const expectedDynamicBudgetChars = Math.floor(
      ctxWindow * RAG_CONTEXT_BUDGET_FRACTION * CHARS_PER_TOKEN,
    );
    // Seed enough pinned insights (unconditional injection — bypasses
    // rag_enabled entirely, see rag_service.py augment_prompt) to
    // GUARANTEE the combined context_block exceeds even the dynamic
    // budget, forcing trim_rag_context_for_model to fire and log the REAL
    // applied budget as `trimmed_chars` — a direct numeric read, not an
    // inference from which secret survived.
    const targetChars = Math.ceil(expectedDynamicBudgetChars * 1.25);
    const neededInsights = Math.min(
      PIN_CAP,
      Math.ceil(targetChars / PIN_TEXT_MAX_LENGTH),
    );
    expect(
      neededInsights * PIN_TEXT_MAX_LENGTH,
      `[dogfood j14c] even ${String(PIN_CAP)} pinned insights at the ` +
        `${String(PIN_TEXT_MAX_LENGTH)}-char cap can't exceed this model's dynamic RAG ` +
        `budget (${String(expectedDynamicBudgetChars)} chars) — cannot force a trim ` +
        "deterministically on this fleet.",
    ).toBeGreaterThan(expectedDynamicBudgetChars);

    const pinnedIds: number[] = [];
    try {
      const fillerUnit = "j14c budget-probe filler content. ";
      const filler = fillerUnit
        .repeat(Math.ceil(PIN_TEXT_MAX_LENGTH / fillerUnit.length))
        .slice(0, PIN_TEXT_MAX_LENGTH - 20);
      for (let i = 0; i < neededInsights; i++) {
        const pinResp = await page.request.post(`${backendURL}/api/memory/pin`, {
          form: { text: `${filler} marker-${String(i)}` },
        });
        expect(
          pinResp.ok(),
          `POST /api/memory/pin[${String(i)}] → HTTP ${String(pinResp.status())}`,
        ).toBe(true);
        const pinBody = (await pinResp.json()) as { id: number };
        pinnedIds.push(pinBody.id);
      }

      const chatId = await createChatViaRequest(
        page,
        backendURL,
        "J14c RAG budget magnitude",
      );
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

      await sendTurnAndWait(
        page,
        "In one short sentence, say hello.",
        TURN_TIMEOUT_MS,
      );

      const gateLine = await waitForLogLine(
        backendLogPath,
        ["stream.rag_context_trimmed", `chat_id=${String(chatId)}`],
        10_000,
      );
      const trimmedMatch = /trimmed_chars=(\d+)/.exec(gateLine);
      expect(
        trimmedMatch,
        "[dogfood j14c] stream.rag_context_trimmed log line had no parseable " +
          `trimmed_chars field: ${gateLine}`,
      ).not.toBeNull();
      const actualTrimmedChars = Number(trimmedMatch?.[1] ?? "0");

      test.info().annotations.push({
        type: "j14c-budget-comparison",
        description:
          `ctxWindow=${String(ctxWindow)} ` +
          `expectedDynamicBudget=${String(expectedDynamicBudgetChars)} ` +
          `actualTrimmedChars=${String(actualTrimmedChars)} ` +
          `oldStaticBudget=${String(OLD_STATIC_BUDGET_CHARS)}`,
      });

      // The applied budget must track the LIVE context window, not sit
      // pinned at the old static-16384-derived ceiling regardless of the
      // real model. A generous margin above the old ceiling (not a tight
      // equality against expectedDynamicBudgetChars) absorbs section-header
      // overhead trim_rag_context_for_model's caller adds on top of the
      // raw pinned text.
      expect(
        actualTrimmedChars,
        `applied RAG budget (${String(actualTrimmedChars)} chars) is at or near the OLD ` +
          `static 16384-token floor's budget (${String(OLD_STATIC_BUDGET_CHARS)} chars) ` +
          `despite a live context window of ${String(ctxWindow)} tokens (expected ~` +
          `${String(expectedDynamicBudgetChars)} chars) — this is the static-budget ` +
          "regression.",
      ).toBeGreaterThan(OLD_STATIC_BUDGET_CHARS * 1.1);
    } finally {
      // MUST run even on assertion failure — pinned insights are
      // unconditional and GLOBAL per user, so leaking any of these huge
      // filler insights would balloon every subsequent journey's prompt
      // for the rest of this (serial, shared-DB) worker run.
      for (const id of pinnedIds) {
        await page.request
          .delete(`${backendURL}/api/memory/pin/${String(id)}`)
          .catch(() => undefined);
      }
    }

    assertNoConsoleErrors(collectErrors(), "j14c");
  },
);
