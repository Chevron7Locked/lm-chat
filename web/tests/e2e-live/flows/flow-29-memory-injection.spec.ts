/**
 * Flow 29 — Memory injection: pinned insight reaches LM Studio system_prompt.
 *
 * What it proves:
 *   Pinned insights (from the Memory page / POST /api/memory/pin) are injected
 *   into the upstream LM Studio request's `system_prompt` UNCONDITIONALLY —
 *   even when `rag_enabled` is false. This is the "explicit user preference"
 *   path in rag_service.augment_prompt:
 *
 *       "Pinned insights from memory_service.list_pinned are injected
 *        unconditionally — they are explicit user preferences, not semantic
 *        recall, and should reach the LLM regardless of
 *        chats.settings.rag_enabled."
 *
 * Where the assertion lives:
 *   Pinned-memory injection is a BACKEND operation (stream_chat → rag_service),
 *   invisible to the FE→backend request. The stub LM Studio captures the
 *   upstream /api/v1/chat body to a per-worker file (chatCaptureFile fixture);
 *   we read it and assert on the real `system_prompt`.
 *
 * Coverage:
 *   29a — pinned insight reaches system_prompt even with rag_enabled=false.
 *   29b — a pinned insight scoped to project A does NOT reach a project-B chat
 *         (the project_id gate on list_pinned).
 *   29c — a deleted pinned insight is absent from subsequent stream context.
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
  "flow-29a: pinned insight reaches LM Studio system_prompt even with rag_enabled=false",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Pin an insight with a unique token.
    const MEMORY_TOKEN = `FLOW29MEMPIN${Date.now().toString()}`;
    const pinResp = await page.request.post(`${backendURL}/api/memory/pin`, {
      form: { text: `My pinned preference token: ${MEMORY_TOKEN}` },
    });
    expect(pinResp.ok()).toBe(true);
    const pinData = (await pinResp.json()) as { id: number; text: string };
    expect(pinData.text).toContain(MEMORY_TOKEN);

    // Create a chat with RAG explicitly DISABLED — proves unconditional injection.
    const chatId = await createChatViaRequest(page, backendURL, "Flow29 Memory Chat");
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
    await composer.fill("Hello, what do you know about me?");
    await composer.press("Meta+Enter");
    await expect(composer).toBeEnabled({ timeout: 20_000 });

    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    // The pinned insight MUST appear even with rag_enabled=false.
    expect(sysPrompt).toContain(MEMORY_TOKEN);
    // rag_service assembles the unconditional section under "## Pinned context".
    expect(sysPrompt).toContain("Pinned context");

    assertNoConsoleErrors(collectErrors(), "flow-29a");
  }
);

test(
  "flow-29b: pinned insight scoped to project A does NOT reach a project-B chat's context",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const pAResp = await page.request.post(`${backendURL}/api/projects`, {
      form: { name: "Flow29bProjA", description: "Memory isolation test A" },
    });
    expect(pAResp.ok()).toBe(true);
    const projA = (await pAResp.json()) as { id: number };
    const projAId = projA.id;

    const pBResp = await page.request.post(`${backendURL}/api/projects`, {
      form: { name: "Flow29bProjB", description: "Memory isolation test B" },
    });
    expect(pBResp.ok()).toBe(true);
    const projB = (await pBResp.json()) as { id: number };
    const projBId = projB.id;

    // Pin an insight SCOPED TO Project A.
    const PA_TOKEN = `FLOW29BPROJATOKEN${Date.now().toString()}`;
    const pinResp = await page.request.post(`${backendURL}/api/memory/pin`, {
      form: {
        text: `Project A scoped insight: ${PA_TOKEN}`,
        project_id: String(projAId),
      },
    });
    expect(pinResp.ok()).toBe(true);

    // Chat in Project B.
    const chatBResp = await page.request.post(
      `${backendURL}/api/projects/${String(projBId)}/chats`,
      { form: { title: "Flow29b ProjB Chat" } }
    );
    expect(chatBResp.ok()).toBe(true);
    const chatB = (await chatBResp.json()) as { id: number };
    const chatBId = chatB.id;

    await page.goto(`${backendURL}/chats/${String(chatBId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await ensureModelSelected(page);

    resetCapturedUpstreamRequest(chatCaptureFile);
    await patchStreamInjectModel(page);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill("What do you know about project A's secrets?");
    await composer.press("Meta+Enter");
    await expect(composer).toBeEnabled({ timeout: 20_000 });

    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    // Project-A scoped insight must NOT appear in a project-B chat.
    expect(sysPrompt).not.toContain(PA_TOKEN);

    assertNoConsoleErrors(collectErrors(), "flow-29b");
  }
);

test(
  "flow-29c: deleting a pinned insight removes it from subsequent stream context",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Pin then delete.
    const DEL_TOKEN = `FLOW29CDEL${Date.now().toString()}`;
    const pinResp = await page.request.post(`${backendURL}/api/memory/pin`, {
      form: { text: `Temporary insight to delete: ${DEL_TOKEN}` },
    });
    expect(pinResp.ok()).toBe(true);
    const pinData = (await pinResp.json()) as { id: number };
    const insightId = pinData.id;

    const deleteResp = await page.request.delete(
      `${backendURL}/api/memory/pin/${String(insightId)}`
    );
    expect(deleteResp.ok()).toBe(true);

    const chatId = await createChatViaRequest(page, backendURL, "Flow29c Delete Memory Chat");

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await ensureModelSelected(page);

    resetCapturedUpstreamRequest(chatCaptureFile);
    await patchStreamInjectModel(page);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill("Hello.");
    await composer.press("Meta+Enter");
    await expect(composer).toBeEnabled({ timeout: 20_000 });

    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    // Deleted insight must NOT appear.
    expect(sysPrompt).not.toContain(DEL_TOKEN);

    assertNoConsoleErrors(collectErrors(), "flow-29c");
  }
);
