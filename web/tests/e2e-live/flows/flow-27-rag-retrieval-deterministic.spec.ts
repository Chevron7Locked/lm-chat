/**
 * Flow 27 — RAG retrieval deterministic: doc chunk text reaches LM Studio.
 *
 * What it proves:
 *   When a document is uploaded, RAG is enabled on a chat, and the user asks
 *   about the document's unique content, the request the BACKEND sends to
 *   LM Studio (the upstream /api/v1/chat body) carries the retrieved doc-chunk
 *   text in its `system_prompt` field.
 *
 * Where the assertion lives — and why:
 *   RAG augmentation happens INSIDE the backend's stream_chat, between the
 *   FE→backend request and the backend→LM Studio request (rag_service.
 *   augment_prompt builds a context_block; streaming_service prepends it to
 *   `system_prompt`). Intercepting the FE fetch would NOT see it. So the stub
 *   LM Studio captures the upstream body to a per-worker file (chatCaptureFile
 *   fixture); we read that file and assert on the real wire payload.
 *
 * Determinism: no real embedding model — the stub's /v1/embeddings endpoint
 * returns a fixed 4-dim unit vector for every input, and the doc chunk embeds
 * with the same vector (cosine 1.0). FTS5 also indexes the literal token, so
 * the keyword pass fires too.
 *
 * First-turn note: the native encoder keeps `system_prompt` only when
 * previous_response_id is absent (first turn). These tests always use a fresh
 * chat, so the RAG block lands in `system_prompt`.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
  patchStreamInjectModel,
  ensureModelSelected,
  resetCapturedUpstreamRequest,
  readCapturedUpstreamRequest,
} from "./_flow-helpers";

test(
  "flow-27a: uploaded doc chunk text reaches LM Studio system_prompt when RAG fires",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Unique token embedded in the document. Single word so FTS5 indexes it
    // as one term and the keyword pass matches when the query repeats it.
    const RAG_TOKEN = `FLOW27RAGTOKEN${Date.now().toString()}`;
    const DOC_TEXT =
      `This document contains the unique retrieval marker: ${RAG_TOKEN}.\n` +
      `It was created specifically for the flow-27 deterministic RAG e2e test.\n`;

    // Upload the plain-text doc via the project-less documents API (multipart).
    const uploadResp = await page.request.post(`${backendURL}/api/documents`, {
      multipart: {
        file: {
          name: "flow27-rag-doc.txt",
          mimeType: "text/plain",
          buffer: Buffer.from(DOC_TEXT, "utf-8"),
        },
      },
    });
    expect(uploadResp.ok()).toBe(true);
    const uploadData = (await uploadResp.json()) as { id: number; chunk_count: number };
    expect(uploadData.chunk_count).toBeGreaterThan(0);

    // Create a chat and explicitly enable RAG.
    const chatId = await createChatViaRequest(page, backendURL, "Flow27 RAG Chat");
    const patchResp = await page.request.patch(`${backendURL}/api/chats/${String(chatId)}`, {
      form: { rag_enabled: "true" },
    });
    expect(patchResp.ok()).toBe(true);

    // Navigate to the chat and get it submit-ready.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await ensureModelSelected(page);

    // Clear any stale capture, patch fetch to carry a model + stream through.
    resetCapturedUpstreamRequest(chatCaptureFile);
    await patchStreamInjectModel(page);

    // Submit a message mentioning the token — this is the retrieval query.
    const composer = page.getByPlaceholder(/Message/);
    await composer.fill(`Tell me about the unique marker ${RAG_TOKEN}`);
    await composer.press("Meta+Enter");

    // Let the stream complete (composer re-enables after chat.end).
    await expect(composer).toBeEnabled({ timeout: 20_000 });

    // Read what the backend sent upstream to LM Studio.
    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    expect(sysPrompt).toContain(RAG_TOKEN);

    assertNoConsoleErrors(collectErrors(), "flow-27a");
  }
);

test(
  "flow-27b: chat with rag_enabled=false does NOT inject doc chunk into LM Studio system_prompt",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const RAG_TOKEN = `FLOW27BRAGTOKEN${Date.now().toString()}`;
    const DOC_TEXT = `Marker that should stay out of context: ${RAG_TOKEN}.\n`;

    const uploadResp = await page.request.post(`${backendURL}/api/documents`, {
      multipart: {
        file: {
          name: "flow27b-nodoc.txt",
          mimeType: "text/plain",
          buffer: Buffer.from(DOC_TEXT, "utf-8"),
        },
      },
    });
    expect(uploadResp.ok()).toBe(true);

    // Create a chat and EXPLICITLY DISABLE RAG. The explicit per-chat toggle
    // wins over the "default ON when the user has docs" smart default, so
    // retrieval is skipped entirely.
    const chatId = await createChatViaRequest(page, backendURL, "Flow27b NoRAG Chat");
    const patchResp = await page.request.patch(`${backendURL}/api/chats/${String(chatId)}`, {
      form: { rag_enabled: "false" },
    });
    expect(patchResp.ok()).toBe(true);

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await ensureModelSelected(page);

    resetCapturedUpstreamRequest(chatCaptureFile);
    await patchStreamInjectModel(page);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill(`What does the document say about ${RAG_TOKEN}?`);
    await composer.press("Meta+Enter");

    await expect(composer).toBeEnabled({ timeout: 20_000 });

    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    // With rag_enabled=false and no pinned memory, the doc token must be absent.
    expect(sysPrompt).not.toContain(RAG_TOKEN);

    assertNoConsoleErrors(collectErrors(), "flow-27b");
  }
);
