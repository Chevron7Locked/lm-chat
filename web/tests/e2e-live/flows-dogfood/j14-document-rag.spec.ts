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
